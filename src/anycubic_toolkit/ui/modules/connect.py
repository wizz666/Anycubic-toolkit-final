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

import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from anycubic_toolkit.core.anycubic_cloud import (
    AnycubicCloudClient,
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
from anycubic_toolkit.core.network_scan import ScanHit, local_subnet_prefix, scan_subnet
from anycubic_toolkit.core.notifications import Notifier, NotifierConfig
from anycubic_toolkit.core.print_history import PrintHistory
from anycubic_toolkit.core import telemetry
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


def _find_lan_match(prefix: str, cloud_model: str) -> str:
    """Look for a LAN-mode printer on the local subnet whose provisioned
    model matches *cloud_model*, for the "link this cloud printer to full
    local control" suggestion. Runs entirely off the UI thread (a subnet
    scan plus provisioning calls). Returns the matching host, or "" if none
    found.

    The cloud API only offers read-only status (no known control endpoint —
    real control goes over cloud MQTT, which needs a client certificate
    baked into Anycubic's own compiled apps, not something this project
    extracts). Pairing a cloud entry with the same printer's LAN-mode
    connection is what actually gets pause/resume/stop and the live temp
    graph working for it, without needing the user to hunt down the IP
    themselves.
    """
    hits = [hit for hit in scan_subnet(prefix) if hit.mode == "lan"]
    if not hits:
        return ""

    target = (cloud_model or "").strip().lower()
    provisioned: list[tuple[ScanHit, str]] = []
    for hit in hits:
        try:
            creds = provision(hit.host)
        except LanError:
            continue
        provisioned.append((hit, (creds.model_name or "").strip().lower()))

    if target:
        for hit, model in provisioned:
            if model and (model == target or model in target or target in model):
                return hit.host

    # No confident model match (or the cloud API didn't report one) - if
    # exactly one LAN-mode printer answered at all, it's still very likely
    # the same one on a typical home network with a couple of printers.
    if len(hits) == 1:
        return hits[0].host
    return ""


class _PrinterCard(Card):
    """One added printer's connection status, with its own refresh/remove controls."""

    removed = Signal(str)
    refresh_requested = Signal(str)
    light_toggle_requested = Signal(str)
    camera_requested = Signal(str)

    def __init__(self, entry: dict[str, Any], parent=None) -> None:
        super().__init__("", parent)
        self.entry_id = str(entry.get("id", ""))
        self.host = str(entry.get("host", ""))
        self.name = str(entry.get("name", "")).strip()
        self._tr: Callable[..., str] | None = None
        self._light_on: bool | None = None
        # Cloud entries have no host of their own (see self.host above) - this
        # is instead the IP a background subnet scan found for the same
        # model, so the user can see it even if they dismissed (or never
        # saw) the one-time "add for local control?" popup.
        self._found_lan_host: str | None = str(entry.get("found_lan_host") or "") or None

        body = self.body_layout()

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.name_label = QLabel(f"<b>{self.name or self.host}</b>")
        self.name_label.setObjectName("CardTitle")
        self.host_label = QLabel(self.host)
        self.host_label.setObjectName("Muted")
        self.host_label.setVisible(bool(self.name))
        self.found_lan_label = QLabel()
        self.found_lan_label.setObjectName("Muted")
        self.found_lan_label.setVisible(False)
        title_col.addWidget(self.name_label)
        title_col.addWidget(self.host_label)
        title_col.addWidget(self.found_lan_label)
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
        self.ace_label = QLabel()
        self.ace_label.setObjectName("Muted")
        self.ace_label.setWordWrap(True)
        self.ace_label.setVisible(False)
        body.addWidget(self.state_label)
        body.addWidget(self.mode_label)
        body.addWidget(self.versions_label)
        body.addWidget(self.temps_label)
        body.addWidget(self.print_label)
        body.addWidget(self.ace_label)

        links = QHBoxLayout()
        self.moonraker_btn = QPushButton()
        self.moonraker_btn.setObjectName("Link")
        self.moonraker_btn.clicked.connect(self._open_moonraker)
        self.moonraker_btn.setVisible(False)
        self.web_btn = QPushButton()
        self.web_btn.setObjectName("Link")
        self.web_btn.clicked.connect(self._open_web)
        self.web_btn.setVisible(False)
        self.light_btn = QPushButton()
        self.light_btn.setObjectName("Link")
        self.light_btn.clicked.connect(lambda: self.light_toggle_requested.emit(self.entry_id))
        self.light_btn.setVisible(False)
        self.camera_btn = QPushButton()
        self.camera_btn.setObjectName("Link")
        self.camera_btn.clicked.connect(lambda: self.camera_requested.emit(self.entry_id))
        self.camera_btn.setVisible(False)
        links.addWidget(self.moonraker_btn)
        links.addWidget(self.web_btn)
        links.addWidget(self.light_btn)
        links.addWidget(self.camera_btn)
        links.addStretch(1)
        body.addLayout(links)

    # ------------------------------------------------------------- language

    def retranslate(self, tr: Callable[..., str]) -> None:
        self._tr = tr
        self.refresh_btn.setText("\N{ANTICLOCKWISE OPEN CIRCLE ARROW} " + tr("connect.refresh"))
        self.remove_btn.setText("\N{CROSS MARK} " + tr("connect.remove"))
        self.moonraker_btn.setText("\N{GLOBE WITH MERIDIANS} " + tr("connect.open_moonraker"))
        self.web_btn.setText("\N{GLOBE WITH MERIDIANS} " + tr("connect.open_web"))
        self.camera_btn.setText("\N{VIDEO CAMERA} " + tr("connect.open_camera"))
        self._update_light_button()
        self._update_found_lan_label()

    def _update_light_button(self) -> None:
        tr = self._tr or (lambda key, **_kw: key)
        key = "connect.light_off" if self._light_on else "connect.light_on"
        self.light_btn.setText("\N{ELECTRIC LIGHT BULB} " + tr(key))

    def _update_found_lan_label(self) -> None:
        tr = self._tr or (lambda key, **_kw: key)
        if self._found_lan_host:
            text = tr("connect.found_on_network", host=self._found_lan_host)
            self.found_lan_label.setText("\N{ROUND PUSHPIN} " + text)
        self.found_lan_label.setVisible(bool(self._found_lan_host))

    def show_found_lan_host(self, host: str) -> None:
        """Persist and display the IP a background subnet scan found for
        this cloud printer's model - called once when the scan first finds
        a match, and again on every future card rebuild via the entry's
        stored ``found_lan_host`` so it survives across app restarts."""
        self._found_lan_host = host
        self._update_found_lan_label()

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
        self.ace_label.setVisible(False)
        self.moonraker_btn.setVisible(False)
        self.web_btn.setVisible(False)
        self.light_btn.setVisible(False)
        self.camera_btn.setVisible(False)

    def show_moonraker(self, status: PrinterStatus) -> None:
        tr = self._tr or (lambda key, **_kw: key)
        self.state_label.setText(tr("connect.online") + f" — {status.state or '?'}")
        self.mode_label.setText(tr("connect.mode_moonraker"))
        self.moonraker_btn.setVisible(True)
        self.web_btn.setVisible(True)
        self.ace_label.setVisible(False)
        self.light_btn.setVisible(False)
        self.camera_btn.setVisible(False)
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

        if status.ace_present and any(slot.material for slot in status.ace_slots):
            parts = [tr("connect.ace_box", temp=status.ace_temp, humidity=status.ace_humidity)]
            for index, slot in enumerate(status.ace_slots, start=1):
                if not slot.material:
                    continue
                marker = "\N{BULLET} " if index == status.ace_loaded_slot else ""
                parts.append(
                    tr("connect.ace_slot", marker=marker, index=index, material=slot.material, percent=int(slot.percent))
                )
            self.ace_label.setText("  ".join(parts))
            self.ace_label.setVisible(True)
        else:
            self.ace_label.setVisible(False)

        if status.light_on is not None:
            self._light_on = status.light_on
            self._update_light_button()
            self.light_btn.setVisible(True)
        else:
            self.light_btn.setVisible(False)
        self.camera_btn.setVisible(bool(status.camera_available))

    def show_cloud(self, printer: CloudPrinter) -> None:
        """Display a read-only cloud snapshot (no LAN-only controls apply here)."""
        tr = self._tr or (lambda key, **_kw: key)
        self.mode_label.setText(tr("connect.mode_cloud"))
        self.moonraker_btn.setVisible(False)
        self.web_btn.setVisible(False)
        self.light_btn.setVisible(False)
        self.camera_btn.setVisible(False)
        self.ace_label.setVisible(False)

        state_text = printer.print_state or ("online" if printer.online else "offline")
        self.state_label.setText(
            (tr("connect.online") + f" — {state_text}") if printer.online else tr("connect.offline")
        )
        self.versions_label.setText(printer.model)
        self.temps_label.setText(tr("connect.temps_cloud", nozzle=printer.nozzle_temp, bed=printer.bed_temp))
        if printer.is_printing:
            self.print_label.setText(
                tr("connect.printing", file=printer.filename or "?", percent=int(printer.progress * 100))
            )
        else:
            self.print_label.setText(tr("connect.print_state", state=printer.print_state or "idle"))

    @property
    def is_light_on(self) -> bool:
        return bool(self._light_on)

    # ------------------------------------------------------------------ links

    def _open_moonraker(self) -> None:
        QDesktopServices.openUrl(QUrl(MoonrakerClient(self.host, DEFAULT_PORT).base_url()))

    def _open_web(self) -> None:
        QDesktopServices.openUrl(QUrl(MoonrakerClient(self.host, DEFAULT_PORT).web_url()))


class _WallTile(Card):
    """One printer's status, shown at a larger, glance-from-across-the-room size."""

    def __init__(self, parent=None) -> None:
        super().__init__("", parent)
        body = self.body_layout()
        self.name_label = QLabel()
        self.name_label.setObjectName("PageTitle")
        self.state_label = QLabel()
        state_font = self.state_label.font()
        state_font.setPointSize(state_font.pointSize() + 3)
        self.state_label.setFont(state_font)
        self.temps_label = QLabel()
        self.temps_label.setWordWrap(True)
        self.print_label = QLabel()
        self.print_label.setWordWrap(True)
        for widget in (self.name_label, self.state_label, self.temps_label, self.print_label):
            body.addWidget(widget)

    def update_info(self, name: str, info: dict[str, str]) -> None:
        self.name_label.setText(name)
        self.state_label.setText(info.get("state", ""))
        self.temps_label.setText(info.get("temps", ""))
        self.print_label.setText(info.get("print", ""))


class WallDashboardWindow(QWidget):
    """A separate, resizable window with large printer tiles for a shop monitor.

    Reads the same cached status the Connect page already fetches (via
    *get_display*) on a short UI-only timer — it never triggers its own
    network polling, so it can't double up requests to the printers.
    """

    def __init__(
        self,
        ctx,
        get_display: Callable[[], dict[str, dict[str, str]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.ctx = ctx
        self._get_display = get_display
        self._tiles: dict[str, _WallTile] = {}
        self.resize(1000, 640)

        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title_label = QLabel(self.tr_("connect.wall_title"))
        self.title_label.setObjectName("PageTitle")
        header.addWidget(self.title_label, 1)
        self.fullscreen_btn = QPushButton(self.tr_("connect.wall_fullscreen"))
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        header.addWidget(self.fullscreen_btn)
        self.close_btn = QPushButton(self.tr_("common.close"))
        self.close_btn.clicked.connect(self.close)
        header.addWidget(self.close_btn)
        outer.addLayout(header)

        self.grid = QGridLayout()
        self.grid.setSpacing(20)
        outer.addLayout(self.grid)
        outer.addStretch(1)

        self.setWindowTitle(self.tr_("connect.wall_title"))
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def tr_(self, key: str, **kwargs: object) -> str:
        return self.ctx.translator.tr(key, **kwargs)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    def refresh(self) -> None:
        printers = self.ctx.config.get("printers", []) or []
        display = self._get_display()
        seen: set[str] = set()
        for entry in printers:
            printer_id = str(entry.get("id", ""))
            if not printer_id:
                continue
            seen.add(printer_id)
            tile = self._tiles.get(printer_id)
            if tile is None:
                tile = _WallTile()
                self._tiles[printer_id] = tile
                index = len(self._tiles) - 1
                self.grid.addWidget(tile, index // 2, index % 2)
            name = str(entry.get("name") or entry.get("host", ""))
            tile.update_info(name, display.get(printer_id, {}))
        for printer_id in list(self._tiles.keys()):
            if printer_id not in seen:
                tile = self._tiles.pop(printer_id)
                self.grid.removeWidget(tile)
                tile.deleteLater()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().closeEvent(event)


class SelectionDialog(QDialog):
    """Generic checkable list picker — used for scan results and cloud printers."""

    def __init__(
        self,
        title: str,
        intro: str,
        labels: list[str],
        accept_text: str,
        close_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)

        layout = QVBoxLayout(self)
        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)

        self.list_widget = QListWidget()
        for label in labels:
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton(close_text)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        accept_btn = QPushButton(accept_text)
        accept_btn.setObjectName("Primary")
        accept_btn.clicked.connect(self.accept)
        buttons.addWidget(accept_btn)
        layout.addLayout(buttons)

        self.resize(440, 320)

    def selected_indices(self) -> list[int]:
        return [
            i
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]


class ConnectPage(ModulePage):
    """Live status for every printer the user has added, via Moonraker or LAN mode."""

    title_key = "connect.title"
    subtitle_key = "connect.subtitle"
    help_key = "connect.help"

    def build(self) -> None:
        self._cards: dict[str, _PrinterCard] = {}
        self._latest_display: dict[str, dict[str, str]] = {}
        self._wall_window: WallDashboardWindow | None = None
        self._history = PrintHistory()
        self._lan_link_checking: set[str] = set()  # cloud entry ids, this session only
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

        scan_row = QHBoxLayout()
        self.scan_btn = QPushButton()
        self.scan_btn.setObjectName("Link")
        self.scan_btn.clicked.connect(self._scan_network)
        scan_row.addWidget(self.scan_btn)
        scan_row.addStretch(1)
        body.addLayout(scan_row)

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

        tools_row = QHBoxLayout()
        self.export_btn = QPushButton()
        self.export_btn.setObjectName("Link")
        self.export_btn.clicked.connect(self._export_printers)
        tools_row.addWidget(self.export_btn)
        self.import_btn = QPushButton()
        self.import_btn.setObjectName("Link")
        self.import_btn.clicked.connect(self._import_printers)
        tools_row.addWidget(self.import_btn)
        tools_row.addStretch(1)
        self.wall_btn = QPushButton()
        self.wall_btn.setObjectName("Link")
        self.wall_btn.clicked.connect(self._open_wall_dashboard)
        tools_row.addWidget(self.wall_btn)
        body.addLayout(tools_row)

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
        self.cloud_exclusive_note = QLabel()
        self.cloud_exclusive_note.setObjectName("Muted")
        self.cloud_exclusive_note.setWordWrap(True)
        cbody.addWidget(self.cloud_exclusive_note)
        cloud_row = QHBoxLayout()
        self.cloud_token_input = QLineEdit()
        self.cloud_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_add_btn = QPushButton()
        self.cloud_add_btn.setObjectName("Primary")
        self.cloud_add_btn.clicked.connect(self._add_cloud_printers)
        cloud_row.addWidget(self.cloud_token_input, 1)
        cloud_row.addWidget(self.cloud_add_btn)
        cbody.addLayout(cloud_row)

        cloud_refresh_row = QHBoxLayout()
        self.cloud_auto_refresh_check = QCheckBox()
        self.cloud_auto_refresh_check.toggled.connect(self._on_cloud_auto_refresh_toggled)
        cloud_refresh_row.addWidget(self.cloud_auto_refresh_check, 1)
        self.cloud_refresh_interval = QComboBox()
        self.cloud_refresh_interval.addItem("", 5)
        self.cloud_refresh_interval.addItem("", 10)
        self.cloud_refresh_interval.currentIndexChanged.connect(
            self._on_cloud_refresh_interval_changed
        )
        cloud_refresh_row.addWidget(self.cloud_refresh_interval)
        cbody.addLayout(cloud_refresh_row)
        self.cloud_refresh_note = QLabel()
        self.cloud_refresh_note.setObjectName("Muted")
        self.cloud_refresh_note.setWordWrap(True)
        cbody.addWidget(self.cloud_refresh_note)

        self.cloud_card.setVisible(False)
        self.content_layout.addWidget(self.cloud_card)
        self.content_layout.addStretch(1)

        self._cloud_poll_timer = QTimer(self)
        self._cloud_poll_timer.timeout.connect(self._poll_cloud)

        self.cloud_auto_refresh_check.setChecked(bool(self.ctx.config.get("cloud_auto_refresh", True)))
        saved_minutes = int(self.ctx.config.get("cloud_refresh_minutes", 5) or 5)
        interval_idx = self.cloud_refresh_interval.findData(saved_minutes)
        self.cloud_refresh_interval.setCurrentIndex(interval_idx if interval_idx >= 0 else 0)

        for entry in self.ctx.config.get("printers", []) or []:
            self._add_card(entry)
            self._refresh_printer(entry.get("id", ""))
        self._sync_empty_state()
        self._apply_cloud_poll_timer()

    def on_shown(self) -> None:
        self._sync_cloud_card()
        self._apply_cloud_poll_timer()
        self._check_cloud_lan_links()
        # Card status text (state/mode/temps/print) is set once by whichever
        # background refresh last completed, formatted with the language
        # active *at that moment* - it isn't touched by retranslate() at
        # all, so without this it stays stuck in the old language (or just
        # stale) until the periodic poll timer or a manual Refresh click
        # happens to fire again. Re-poll every card whenever this page
        # becomes visible - covers both a language switch and simply
        # navigating back to this page after a while.
        for entry in self.ctx.config.get("printers", []) or []:
            entry_id = entry.get("id", "")
            if entry_id:
                self._refresh_printer(entry_id)

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
        self.scan_btn.setText("\N{LEFT-POINTING MAGNIFYING GLASS} " + self.tr_("connect.scan"))
        self.monitor_check.setText(self.tr_("connect.monitor"))
        self.refresh_all_btn.setText(
            "\N{ANTICLOCKWISE OPEN CIRCLE ARROW} " + self.tr_("connect.refresh_all")
        )
        self.export_btn.setText("\N{FLOPPY DISK} " + self.tr_("connect.export"))
        self.import_btn.setText("\N{OPEN FILE FOLDER} " + self.tr_("connect.import"))
        self.wall_btn.setText("\N{TELEVISION} " + self.tr_("connect.wall_dashboard"))
        self.printers_title.setText(self.tr_("connect.live_title"))
        self.empty_label.setText(self.tr_("connect.empty"))
        for card in self._cards.values():
            card.retranslate(self.tr_)
        self.cloud_card.set_title(self.tr_("connect.cloud_title"))
        self.cloud_exclusive_note.setText(
            "\N{INFORMATION SOURCE} " + self.tr_("connect.cloud_exclusive_note")
        )
        self.cloud_token_input.setPlaceholderText(self.tr_("connect.cloud_token_placeholder"))
        self.cloud_add_btn.setText("\N{CLOUD} " + self.tr_("connect.cloud_add"))
        self.cloud_auto_refresh_check.setText(self.tr_("connect.cloud_auto_refresh"))
        self.cloud_refresh_interval.setItemText(0, self.tr_("connect.cloud_every_5"))
        self.cloud_refresh_interval.setItemText(1, self.tr_("connect.cloud_every_10"))
        self.cloud_refresh_note.setText(self.tr_("connect.cloud_refresh_note"))
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
        self._apply_cloud_poll_timer()

    def _add_card(self, entry: dict[str, Any]) -> None:
        card = _PrinterCard(entry)
        card.removed.connect(self._remove_printer)
        card.refresh_requested.connect(lambda pid: self._refresh_printer(pid, manual=True))
        card.light_toggle_requested.connect(self._toggle_light)
        card.camera_requested.connect(self._open_camera)
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

    # ---------------------------------------------------------- export/import

    def _export_printers(self) -> None:
        printers = self.ctx.config.get("printers", []) or []
        path, _filter = QFileDialog.getSaveFileName(
            self, self.tr_("connect.export"), "anycubic-toolkit-printers.json", "JSON (*.json)"
        )
        if not path:
            return
        # Only name/host, and only IP-based printers — never LAN-mode
        # credentials (device cert/key, MQTT password), which are per-pairing
        # secrets, and never cloud entries (re-added via "Add cloud printers"
        # instead, since they need re-matching against the account anyway).
        data = [
            {"name": p.get("name", ""), "host": p.get("host", "")}
            for p in printers
            if p.get("kind") != "cloud"
        ]
        try:
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.status_label.setText(self.tr_("connect.export_error", reason=str(exc)))
            return
        self.status_label.setText(self.tr_("connect.export_done", count=len(data), path=path))

    def _import_printers(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, self.tr_("connect.import"), "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.status_label.setText(self.tr_("connect.import_invalid"))
            return
        if not isinstance(data, list):
            self.status_label.setText(self.tr_("connect.import_invalid"))
            return

        printers = list(self.ctx.config.get("printers", []) or [])
        existing_hosts = {p.get("host", "").lower() for p in printers}
        added = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host", "")).strip()
            if not host or host.lower() in existing_hosts:
                continue
            entry = {"id": uuid4().hex[:8], "name": str(item.get("name", "")).strip(), "host": host}
            printers.append(entry)
            existing_hosts.add(host.lower())
            added += 1
            self._add_card(entry)

        if added:
            self.ctx.config.set("printers", printers)
            self._sync_empty_state()
            for entry in printers[-added:]:
                self._refresh_printer(entry["id"], manual=True)
        self.status_label.setText(self.tr_("connect.import_done", count=added))

    # ------------------------------------------------------------------ scan

    def _scan_network(self) -> None:
        prefix = local_subnet_prefix()
        if not prefix:
            self.status_label.setText(self.tr_("connect.scan_no_network"))
            return
        self.scan_btn.setEnabled(False)
        self.status_label.setText(self.tr_("connect.scanning", progress="0/254"))
        worker = FunctionWorker(scan_subnet, prefix)
        worker.signals.progress.connect(self._on_scan_progress)
        worker.signals.finished.connect(self._on_scan_finished)
        worker.signals.error.connect(self._on_scan_error)
        run_in_background(worker)

    def _on_scan_progress(self, _percent: int, description: str) -> None:
        self.status_label.setText(self.tr_("connect.scanning", progress=description))

    def _on_scan_error(self, message: str) -> None:
        self.scan_btn.setEnabled(True)
        self.status_label.setText(self.tr_("connect.scan_error", reason=message))

    def _on_scan_finished(self, hits: list[ScanHit]) -> None:
        self.scan_btn.setEnabled(True)
        printers = self.ctx.config.get("printers", []) or []
        existing_hosts = {p.get("host", "").lower() for p in printers}
        new_hits = [h for h in hits if h.host.lower() not in existing_hosts]

        if not hits:
            self.status_label.setText(self.tr_("connect.scan_none_found"))
            return
        if not new_hits:
            self.status_label.setText(self.tr_("connect.scan_all_already_added", count=len(hits)))
            return

        labels = []
        for hit in new_hits:
            mode_text = self.tr_("connect.mode_moonraker" if hit.mode == "moonraker" else "connect.mode_lan")
            label = hit.host if not hit.name else f"{hit.host} — {hit.name}"
            labels.append(f"{label}  ({mode_text})")
        dialog = SelectionDialog(
            self.tr_("connect.scan_results_title"),
            self.tr_("connect.scan_results_intro", count=len(new_hits)),
            labels,
            self.tr_("connect.scan_add_selected"),
            self.tr_("common.close"),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("")
            return

        selected = [new_hits[i] for i in dialog.selected_indices()]
        printers = list(self.ctx.config.get("printers", []) or [])
        new_entries = []
        for hit in selected:
            entry = {"id": uuid4().hex[:8], "name": hit.name, "host": hit.host}
            printers.append(entry)
            new_entries.append(entry)
            self._add_card(entry)

        if new_entries:
            self.ctx.config.set("printers", printers)
            self._sync_empty_state()
            for entry in new_entries:
                self._refresh_printer(entry["id"], manual=True)
        self.status_label.setText(self.tr_("connect.scan_added", count=len(new_entries)))

    # ---------------------------------------------------------- wall display

    def _open_wall_dashboard(self) -> None:
        if self._wall_window is None:
            self._wall_window = WallDashboardWindow(self.ctx, lambda: self._latest_display, self)
            self._wall_window.destroyed.connect(self._on_wall_window_closed)
        self._wall_window.show()
        self._wall_window.raise_()
        self._wall_window.activateWindow()

    def _on_wall_window_closed(self) -> None:
        self._wall_window = None

    # ------------------------------------------------------------- fetching

    def _refresh_all(self) -> None:
        for printer_id in list(self._cards.keys()):
            self._refresh_printer(printer_id, manual=True)

    def _poll_all(self) -> None:
        # Cloud printers have their own, much slower poll timer (see
        # _poll_cloud) — polling Anycubic's cloud API every 20s like a local
        # printer would be wasteful and risks rate limits.
        for printer_id in list(self._cards.keys()):
            entry = self._entry_by_id(printer_id)
            if entry is not None and entry.get("kind") == "cloud":
                continue
            self._refresh_printer(printer_id)

    def _poll_cloud(self) -> None:
        for printer_id in list(self._cards.keys()):
            entry = self._entry_by_id(printer_id)
            if entry is not None and entry.get("kind") == "cloud":
                self._refresh_printer(printer_id)

    def _has_cloud_printers(self) -> bool:
        return any(
            p.get("kind") == "cloud" for p in (self.ctx.config.get("printers", []) or [])
        )

    def _apply_cloud_poll_timer(self) -> None:
        enabled = self.cloud_auto_refresh_check.isChecked()
        minutes = int(self.cloud_refresh_interval.currentData() or 5)
        if enabled and self._has_cloud_printers():
            self._cloud_poll_timer.setInterval(minutes * 60_000)
            if not self._cloud_poll_timer.isActive():
                self._cloud_poll_timer.start()
        else:
            self._cloud_poll_timer.stop()

    def _on_cloud_auto_refresh_toggled(self, checked: bool) -> None:
        self.ctx.config.set("cloud_auto_refresh", checked)
        self._apply_cloud_poll_timer()

    def _on_cloud_refresh_interval_changed(self, _index: int) -> None:
        minutes = self.cloud_refresh_interval.currentData()
        if minutes:
            self.ctx.config.set("cloud_refresh_minutes", int(minutes))
        self._apply_cloud_poll_timer()

    def _refresh_printer(self, printer_id: str, manual: bool = False) -> None:
        entry = self._entry_by_id(printer_id)
        card = self._cards.get(printer_id)
        if entry is None or card is None:
            return
        if entry.get("kind") == "cloud":
            self._refresh_cloud_printer(printer_id, entry, manual=manual)
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
            return
        self._latest_display[printer_id] = {
            "state": card.state_label.text(),
            "mode": card.mode_label.text(),
            "temps": card.temps_label.text(),
            "print": card.print_label.text(),
        }

    def _show_offline(self, printer_id: str, text: str) -> None:
        card = self._cards.get(printer_id)
        if card is not None:
            card.show_offline(text)
        self._latest_display[printer_id] = {"state": text, "mode": "", "temps": "", "print": ""}

    # ---------------------------------------------------------- light/camera

    def _toggle_light(self, printer_id: str) -> None:
        entry = self._entry_by_id(printer_id)
        card = self._cards.get(printer_id)
        if entry is None or card is None:
            return
        host = str(entry.get("host", ""))
        turn_on = not card.is_light_on
        worker = FunctionWorker(self._set_light, host, turn_on)
        worker.signals.finished.connect(
            lambda status, pid=printer_id: self._apply_lan_update(pid, status)
        )
        worker.signals.error.connect(
            lambda msg: self.status_label.setText(self.tr_("connect.light_error", reason=msg))
        )
        run_in_background(worker)

    def _set_light(self, host: str, turn_on: bool) -> LanPrinterStatus:
        creds = self._lan_credentials(host)
        return AnycubicLanClient(creds).fetch_status(collect_seconds=3.0, set_light=(turn_on, 100))

    def _open_camera(self, printer_id: str) -> None:
        entry = self._entry_by_id(printer_id)
        if entry is None:
            return
        host = str(entry.get("host", ""))
        self.status_label.setText(self.tr_("connect.camera_starting"))
        worker = FunctionWorker(self._start_camera, host)
        worker.signals.finished.connect(
            lambda status, pid=printer_id: self._on_camera_started(pid, status)
        )
        worker.signals.error.connect(
            lambda msg: self.status_label.setText(self.tr_("connect.camera_error", reason=msg))
        )
        run_in_background(worker)

    def _start_camera(self, host: str) -> LanPrinterStatus:
        creds = self._lan_credentials(host)
        return AnycubicLanClient(creds).fetch_status(collect_seconds=4.0, start_camera=True)

    def _on_camera_started(self, printer_id: str, status: LanPrinterStatus) -> None:
        self._apply_lan_update(printer_id, status)
        if status.camera_url:
            self.status_label.setText("")
            QDesktopServices.openUrl(QUrl(status.camera_url))
        else:
            self.status_label.setText(self.tr_("connect.camera_no_url"))

    def _apply_lan_update(self, printer_id: str, status: LanPrinterStatus) -> None:
        card = self._cards.get(printer_id)
        if card is not None and status.online:
            card.show_lan(status)

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
        elif mode == "cloud" and isinstance(status, CloudPrinter) and status.online:
            snap = telemetry.from_cloud(status, host)
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

    def _add_cloud_printers(self) -> None:
        token = self.cloud_token_input.text().strip()
        if not token:
            self.status_label.setText(self.tr_("connect.cloud_hint_no_token"))
            return
        self.ctx.config.set("cloud_access_token", token)
        self.cloud_add_btn.setEnabled(False)
        self.status_label.setText(self.tr_("connect.connecting"))
        worker = FunctionWorker(self._fetch_cloud_list, token)
        worker.signals.finished.connect(self._on_cloud_list_fetched)
        worker.signals.error.connect(self._on_cloud_list_error)
        run_in_background(worker)

    def _fetch_cloud_list(self, token: str) -> list[CloudPrinter]:
        return AnycubicCloudClient(token).printers()

    def _on_cloud_list_error(self, message: str) -> None:
        self.cloud_add_btn.setEnabled(True)
        self.status_label.setText(self.tr_("connect.cloud_error", reason=message))

    def _on_cloud_list_fetched(self, printers: list[CloudPrinter]) -> None:
        self.cloud_add_btn.setEnabled(True)
        if not printers:
            self.status_label.setText(self.tr_("connect.cloud_no_printers"))
            return

        existing_ids = {
            p.get("cloud_id")
            for p in (self.ctx.config.get("printers", []) or [])
            if p.get("kind") == "cloud"
        }
        new_printers = [p for p in printers if p.id and p.id not in existing_ids]
        if not new_printers:
            self.status_label.setText(
                self.tr_("connect.scan_all_already_added", count=len(printers))
            )
            return

        labels = []
        for printer in new_printers:
            name = printer.name or printer.model or "?"
            suffix = f" — {printer.model}" if printer.model and printer.model != name else ""
            labels.append(f"{name}{suffix}")
        dialog = SelectionDialog(
            self.tr_("connect.cloud_picker_title"),
            self.tr_("connect.cloud_picker_intro", count=len(new_printers)),
            labels,
            self.tr_("connect.scan_add_selected"),
            self.tr_("common.close"),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("")
            return

        selected = [new_printers[i] for i in dialog.selected_indices()]
        printers_cfg = list(self.ctx.config.get("printers", []) or [])
        for printer in selected:
            entry = {
                "id": uuid4().hex[:8],
                "name": printer.name or printer.model,
                "kind": "cloud",
                "cloud_id": printer.id,
            }
            printers_cfg.append(entry)
            self._add_card(entry)
            self._show_cloud_result(entry["id"], printer)
        if selected:
            self.ctx.config.set("printers", printers_cfg)
            self._sync_empty_state()
            self._apply_cloud_poll_timer()
        self.status_label.setText(self.tr_("connect.scan_added", count=len(selected)))
        self._check_cloud_lan_links()

    # ---------------------------------------------------- cloud/LAN linking

    def _check_cloud_lan_links(self) -> None:
        """For every cloud printer without a known local IP yet, look for
        the same model on the local network and, if found, offer to add it
        too — giving that printer full local control (pause/resume/stop,
        live temps) alongside its cloud (away-from-home) status.

        ``lan_link_checked`` alone used to gate this permanently after the
        first attempt - fine for "don't re-show the popup" but it also
        meant an entry that found nothing the first time (e.g. the printer
        was in cloud mode back then) would never get a second look, even
        after switching to LAN mode later. Now only entries that have
        already found and stored a host are skipped; everything else gets
        rechecked on every page load so the found-IP display in
        _PrinterCard can actually catch up once the printer becomes
        reachable."""
        prefix = local_subnet_prefix()
        if not prefix:
            return
        for entry in self.ctx.config.get("printers", []) or []:
            if entry.get("kind") != "cloud" or entry.get("found_lan_host"):
                continue
            entry_id = entry.get("id", "")
            if not entry_id or entry_id in self._lan_link_checking:
                continue
            self._lan_link_checking.add(entry_id)
            model = str(entry.get("model") or entry.get("name") or "")
            worker = FunctionWorker(_find_lan_match, prefix, model)
            worker.signals.finished.connect(
                lambda host, eid=entry_id: self._on_lan_link_result(eid, host)
            )
            worker.signals.error.connect(lambda _msg, eid=entry_id: self._lan_link_checking.discard(eid))
            run_in_background(worker)

    def _on_lan_link_result(self, cloud_entry_id: str, host: str) -> None:
        self._lan_link_checking.discard(cloud_entry_id)
        if not host:
            return
        printers = self.ctx.config.get("printers", []) or []
        if any(p.get("host", "").lower() == host.lower() for p in printers):
            return  # already added by some other path in the meantime
        entry = self._entry_by_id(cloud_entry_id)
        if entry is None:
            return
        name = str(entry.get("name") or host)

        # Show/persist the found IP on the cloud card right away, regardless
        # of what the user does with the popup below - dismissing it (or
        # never seeing it) shouldn't lose the one useful fact it found.
        self._save_found_lan_host(cloud_entry_id, host)
        card = self._cards.get(cloud_entry_id)
        if card is not None:
            card.show_found_lan_host(host)

        dialog = SelectionDialog(
            self.tr_("connect.lan_link_title"),
            self.tr_("connect.lan_link_intro", name=name, host=host),
            [self.tr_("connect.lan_link_option", host=host)],
            self.tr_("connect.lan_link_add"),
            self.tr_("common.close"),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_indices():
            return

        printers = list(self.ctx.config.get("printers", []) or [])
        new_entry = {"id": uuid4().hex[:8], "name": name, "host": host}
        printers.append(new_entry)
        self.ctx.config.set("printers", printers)
        self._add_card(new_entry)
        self._sync_empty_state()
        self._refresh_printer(new_entry["id"], manual=True)
        self.status_label.setText(self.tr_("connect.lan_link_added", name=name))

    def _save_found_lan_host(self, cloud_entry_id: str, host: str) -> None:
        printers = list(self.ctx.config.get("printers", []) or [])
        changed = False
        for entry in printers:
            if entry.get("id") == cloud_entry_id and entry.get("found_lan_host") != host:
                entry["found_lan_host"] = host
                changed = True
        if changed:
            self.ctx.config.set("printers", printers)

    def _refresh_cloud_printer(self, printer_id: str, entry: dict[str, Any], manual: bool = False) -> None:
        card = self._cards.get(printer_id)
        if card is None:
            return
        token = str(self.ctx.config.get("cloud_access_token", "") or "").strip()
        if not token:
            card.show_offline(self.tr_("connect.cloud_hint_no_token"))
            return
        if manual:
            card.show_connecting(self.tr_("connect.connecting"))
        cloud_id = str(entry.get("cloud_id", ""))
        worker = FunctionWorker(self._fetch_one_cloud_printer, token, cloud_id)
        worker.signals.finished.connect(
            lambda printer, pid=printer_id: self._show_cloud_result(pid, printer)
        )
        worker.signals.error.connect(
            lambda msg, pid=printer_id: self._show_offline(pid, self.tr_("connect.cloud_error", reason=msg))
        )
        run_in_background(worker)

    def _fetch_one_cloud_printer(self, token: str, cloud_id: str) -> CloudPrinter | None:
        for printer in AnycubicCloudClient(token).printers():
            if printer.id == cloud_id:
                return printer
        return None

    def _show_cloud_result(self, printer_id: str, printer: CloudPrinter | None) -> None:
        card = self._cards.get(printer_id)
        if card is None:
            return
        if printer is None:
            self._show_offline(printer_id, self.tr_("connect.offline"))
            return
        card.show_cloud(printer)
        self._feed_monitor(printer_id, "cloud", printer)
        self._latest_display[printer_id] = {
            "state": card.state_label.text(),
            "mode": card.mode_label.text(),
            "temps": card.temps_label.text(),
            "print": card.print_label.text(),
        }
