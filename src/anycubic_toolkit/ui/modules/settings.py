"""Settings: language, theme, update channel, auto-update and folders."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)

from anycubic_toolkit.core.config import config_dir
from anycubic_toolkit.core.ha_publisher import HaConfig, HomeAssistantPublisher
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class SettingsPage(ModulePage):
    """Persistent application preferences."""

    title_key = "settings.title"
    subtitle_key = "settings.subtitle"
    help_key = "settings.help"

    def build(self) -> None:
        self._loading = False
        self.card = Card()
        body = self.card.body_layout()

        # Language
        self.language_label = QLabel()
        self.language_combo = QComboBox()
        for code, name in self.ctx.translator.available_languages():
            self.language_combo.addItem(name, code)
        self.language_combo.currentIndexChanged.connect(self._on_language)
        body.addWidget(self.language_label)
        body.addWidget(self.language_combo)

        # Theme
        self.theme_label = QLabel()
        self.theme_combo = QComboBox()
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        body.addWidget(self.theme_label)
        body.addWidget(self.theme_combo)

        # Update channel
        self.channel_label = QLabel()
        self.channel_combo = QComboBox()
        self.channel_combo.currentIndexChanged.connect(self._on_channel)
        body.addWidget(self.channel_label)
        body.addWidget(self.channel_combo)

        # Auto update
        self.auto_update_check = QCheckBox()
        self.auto_update_check.toggled.connect(self._on_auto_update)
        body.addWidget(self.auto_update_check)

        # Opt-in Anycubic Cloud (unofficial, read-only) — off by default.
        self.cloud_check = QCheckBox()
        self.cloud_check.toggled.connect(self._on_cloud_toggled)
        body.addWidget(self.cloud_check)
        self.cloud_note = QLabel()
        self.cloud_note.setObjectName("Muted")
        self.cloud_note.setWordWrap(True)
        body.addWidget(self.cloud_note)

        # Download folder
        self.folder_label = QLabel()
        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setReadOnly(True)
        self.folder_btn = QPushButton()
        self.folder_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_input, 1)
        folder_row.addWidget(self.folder_btn)
        body.addWidget(self.folder_label)
        body.addLayout(folder_row)

        # Open config folder
        self.open_config_btn = QPushButton()
        self.open_config_btn.setObjectName("Link")
        self.open_config_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_dir())))
        )
        body.addWidget(self.open_config_btn)

        self.content_layout.addWidget(self.card)

        # ---------------------------------------------- notifications card
        self.notify_card = Card()
        nbody = self.notify_card.body_layout()
        self.notify_check = QCheckBox()
        self.notify_check.toggled.connect(self._on_notify_toggled)
        nbody.addWidget(self.notify_check)
        self.notify_note = QLabel()
        self.notify_note.setObjectName("Muted")
        self.notify_note.setWordWrap(True)
        nbody.addWidget(self.notify_note)
        provider_row = QHBoxLayout()
        self.notify_provider_label = QLabel()
        self.notify_provider = QComboBox()
        self.notify_provider.addItem("ntfy", "ntfy")
        self.notify_provider.addItem("Discord", "discord")
        self.notify_provider.addItem("Webhook", "webhook")
        self.notify_provider.currentIndexChanged.connect(self._on_notify_provider)
        provider_row.addWidget(self.notify_provider_label)
        provider_row.addWidget(self.notify_provider, 1)
        nbody.addLayout(provider_row)
        self.notify_target = QLineEdit()
        self.notify_target.editingFinished.connect(self._on_notify_target)
        nbody.addWidget(self.notify_target)
        self.notify_server = QLineEdit()
        self.notify_server.editingFinished.connect(self._on_notify_server)
        nbody.addWidget(self.notify_server)
        self.content_layout.addWidget(self.notify_card)

        # ------------------------------------------- home assistant card
        self.ha_card = Card()
        hbody = self.ha_card.body_layout()
        self.ha_check = QCheckBox()
        self.ha_check.toggled.connect(self._on_ha_toggled)
        hbody.addWidget(self.ha_check)
        self.ha_note = QLabel()
        self.ha_note.setObjectName("Muted")
        self.ha_note.setWordWrap(True)
        hbody.addWidget(self.ha_note)
        host_row = QHBoxLayout()
        self.ha_host = QLineEdit()
        self.ha_host.editingFinished.connect(lambda: self.ctx.config.set("ha_host", self.ha_host.text().strip()))
        self.ha_port = QLineEdit()
        self.ha_port.setFixedWidth(72)
        self.ha_port.editingFinished.connect(lambda: self.ctx.config.set("ha_port", self.ha_port.text().strip() or "1883"))
        host_row.addWidget(self.ha_host, 1)
        host_row.addWidget(self.ha_port)
        hbody.addLayout(host_row)
        cred_row = QHBoxLayout()
        self.ha_user = QLineEdit()
        self.ha_user.editingFinished.connect(lambda: self.ctx.config.set("ha_username", self.ha_user.text().strip()))
        self.ha_pass = QLineEdit()
        self.ha_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.ha_pass.editingFinished.connect(lambda: self.ctx.config.set("ha_password", self.ha_pass.text()))
        cred_row.addWidget(self.ha_user, 1)
        cred_row.addWidget(self.ha_pass, 1)
        hbody.addLayout(cred_row)
        test_row = QHBoxLayout()
        self.ha_test_btn = QPushButton()
        self.ha_test_btn.clicked.connect(self._on_ha_test)
        self.ha_test_result = QLabel()
        self.ha_test_result.setObjectName("Muted")
        test_row.addWidget(self.ha_test_btn)
        test_row.addWidget(self.ha_test_result, 1)
        hbody.addLayout(test_row)
        self.content_layout.addWidget(self.ha_card)
        self.content_layout.addStretch(1)

        self._load_values()

    def retranslate(self) -> None:
        super().retranslate()
        self.card.set_title(self.tr_("settings.title"))
        self.language_label.setText(self.tr_("settings.language"))
        self.theme_label.setText(self.tr_("settings.theme"))
        self.channel_label.setText(self.tr_("settings.update_channel"))
        self.auto_update_check.setText(self.tr_("settings.auto_update"))
        self.cloud_check.setText(self.tr_("settings.cloud_toggle"))
        self.cloud_note.setText(self.tr_("settings.cloud_note"))
        self.notify_card.set_title(self.tr_("settings.notify_title"))
        self.notify_check.setText(self.tr_("settings.notify_toggle"))
        self.notify_note.setText(self.tr_("settings.notify_note"))
        self.notify_provider_label.setText(self.tr_("settings.notify_provider"))
        self.notify_target.setPlaceholderText(self.tr_("settings.notify_target_placeholder"))
        self.notify_server.setPlaceholderText(self.tr_("settings.notify_server_placeholder"))
        self.ha_card.set_title(self.tr_("settings.ha_title"))
        self.ha_check.setText(self.tr_("settings.ha_toggle"))
        self.ha_note.setText(self.tr_("settings.ha_note"))
        self.ha_host.setPlaceholderText(self.tr_("settings.ha_host_placeholder"))
        self.ha_port.setPlaceholderText("1883")
        self.ha_user.setPlaceholderText(self.tr_("settings.ha_user_placeholder"))
        self.ha_pass.setPlaceholderText(self.tr_("settings.ha_pass_placeholder"))
        self.ha_test_btn.setText(self.tr_("settings.ha_test"))
        self.folder_label.setText(self.tr_("settings.download_folder"))
        self.folder_btn.setText(self.tr_("settings.browse"))
        self.open_config_btn.setText(self.tr_("settings.open_config"))
        self._reload_theme_labels()
        self._reload_channel_labels()

    # ------------------------------------------------------------- loading

    def _load_values(self) -> None:
        self._loading = True

        current_lang = self.ctx.translator.language
        index = self.language_combo.findData(current_lang)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)

        self._reload_theme_labels()
        self._reload_channel_labels()

        self.auto_update_check.setChecked(bool(self.ctx.config.get("auto_update")))
        self.cloud_check.setChecked(bool(self.ctx.config.get("cloud_enabled")))
        self.notify_check.setChecked(bool(self.ctx.config.get("notify_enabled")))
        idx = self.notify_provider.findData(str(self.ctx.config.get("notify_provider", "ntfy")))
        if idx >= 0:
            self.notify_provider.blockSignals(True)
            self.notify_provider.setCurrentIndex(idx)
            self.notify_provider.blockSignals(False)
        self.notify_target.setText(str(self.ctx.config.get("notify_target", "") or ""))
        self.notify_server.setText(str(self.ctx.config.get("notify_ntfy_server", "") or ""))
        self.notify_server.setVisible(self.notify_provider.currentData() == "ntfy")
        self.ha_check.setChecked(bool(self.ctx.config.get("ha_enabled")))
        self.ha_host.setText(str(self.ctx.config.get("ha_host", "") or ""))
        self.ha_port.setText(str(self.ctx.config.get("ha_port", "1883") or "1883"))
        self.ha_user.setText(str(self.ctx.config.get("ha_username", "") or ""))
        self.ha_pass.setText(str(self.ctx.config.get("ha_password", "") or ""))
        self.folder_input.setText(str(self.ctx.config.get("download_folder")))

        self._loading = False

    def _reload_theme_labels(self) -> None:
        was_loading = self._loading
        self._loading = True
        self.theme_combo.clear()
        for theme in self.ctx.theme.available_themes():
            label = self.tr_(f"topbar.theme_{theme}") if theme in ("dark", "light") else theme
            self.theme_combo.addItem(label, theme)
        index = self.theme_combo.findData(self.ctx.theme.theme)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)
        self._loading = was_loading

    def _reload_channel_labels(self) -> None:
        was_loading = self._loading
        self._loading = True
        current = self.ctx.config.get("update_channel")
        self.channel_combo.clear()
        self.channel_combo.addItem(self.tr_("settings.channel_stable"), "stable")
        self.channel_combo.addItem(self.tr_("settings.channel_beta"), "beta")
        index = self.channel_combo.findData(current)
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)
        self._loading = was_loading

    # ------------------------------------------------------------- handlers

    def _on_language(self) -> None:
        if self._loading:
            return
        code = self.language_combo.currentData()
        if code:
            self.ctx.config.set("language", code)
            self.ctx.translator.set_language(code)

    def _on_theme(self) -> None:
        if self._loading:
            return
        theme = self.theme_combo.currentData()
        if theme:
            self.ctx.config.set("theme", theme)
            self.ctx.theme.apply(theme)

    def _on_channel(self) -> None:
        if self._loading:
            return
        channel = self.channel_combo.currentData()
        if channel:
            self.ctx.config.set("update_channel", channel)

    def _on_auto_update(self, checked: bool) -> None:
        if self._loading:
            return
        self.ctx.config.set("auto_update", checked)

    def _on_cloud_toggled(self, checked: bool) -> None:
        self.ctx.config.set("cloud_enabled", bool(checked))
        if not checked:
            # Leaving cloud mode: forget the stored token.
            self.ctx.config.set("cloud_access_token", "")

    def _on_notify_toggled(self, checked: bool) -> None:
        self.ctx.config.set("notify_enabled", bool(checked))

    def _on_notify_provider(self, _index: int) -> None:
        provider = str(self.notify_provider.currentData() or "ntfy")
        self.ctx.config.set("notify_provider", provider)
        self.notify_server.setVisible(provider == "ntfy")

    def _on_notify_target(self) -> None:
        self.ctx.config.set("notify_target", self.notify_target.text().strip())

    def _on_notify_server(self) -> None:
        self.ctx.config.set(
            "notify_ntfy_server", self.notify_server.text().strip() or "https://ntfy.sh"
        )

    def _on_ha_toggled(self, checked: bool) -> None:
        self.ctx.config.set("ha_enabled", bool(checked))

    def _on_ha_test(self) -> None:
        self.ha_test_result.setText(self.tr_("common.loading"))
        publisher = HomeAssistantPublisher(HaConfig.from_config(self.ctx.config))
        worker = FunctionWorker(publisher.test_connection)
        worker.signals.finished.connect(self._show_ha_test)
        worker.signals.error.connect(
            lambda _msg: self.ha_test_result.setText(self.tr_("settings.ha_test_fail"))
        )
        run_in_background(worker)

    def _show_ha_test(self, result: tuple[bool, str]) -> None:
        ok, message = result
        if ok:
            self.ha_test_result.setText("\N{CHECK MARK} " + self.tr_("settings.ha_test_ok"))
        elif message == "ha_missing_mqtt":
            self.ha_test_result.setText(self.tr_("settings.ha_test_missing"))
        else:
            self.ha_test_result.setText(
                self.tr_("settings.ha_test_fail") + f" ({message})"
            )

    def _choose_folder(self) -> None:
        current = self.ctx.config.get("download_folder")
        folder = QFileDialog.getExistingDirectory(
            self, self.tr_("settings.download_folder"), current
        )
        if folder:
            self.ctx.config.set("download_folder", folder)
            self.folder_input.setText(folder)
