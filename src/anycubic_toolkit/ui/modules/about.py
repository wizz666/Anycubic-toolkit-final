"""About: version, description, links and licensing."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPushButton

from anycubic_toolkit import (
    __homepage__,
    __license__,
    __sponsor_github__,
    __version__,
)
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class AboutPage(ModulePage):
    """Static information about the application."""

    title_key = "about.title"
    subtitle_key = ""
    help_key = "about.help"

    def build(self) -> None:
        self.card = Card()
        body = self.card.body_layout()

        self.logo = QLabel("\N{PRINTER}")
        self.logo.setStyleSheet("font-size: 40px;")
        self.logo.setAlignment(Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.logo)

        self.description = QLabel()
        self.description.setWordWrap(True)
        body.addWidget(self.description)

        self.version_label = QLabel()
        self.version_label.setObjectName("Muted")
        self.license_label = QLabel()
        self.license_label.setObjectName("Muted")
        body.addWidget(self.version_label)
        body.addWidget(self.license_label)

        self.github_btn = QPushButton()
        self.github_btn.setObjectName("Link")
        self.github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(__homepage__))
        )
        self.donate_btn = QPushButton()
        self.donate_btn.setObjectName("Link")
        self.donate_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(__sponsor_github__))
        )
        body.addWidget(self.github_btn)
        body.addWidget(self.donate_btn)

        self.made_with = QLabel()
        self.made_with.setObjectName("Muted")
        self.made_with.setWordWrap(True)
        body.addWidget(self.made_with)

        self.content_layout.addWidget(self.card)
        self.content_layout.addStretch(1)

    def retranslate(self) -> None:
        super().retranslate()
        self.card.set_title(self.tr_("app.name"))
        self.description.setText(self.tr_("about.description"))
        self.version_label.setText(
            f"{self.tr_('about.version')}: {__version__}"
        )
        self.license_label.setText(
            f"{self.tr_('about.license')}: {__license__}"
        )
        self.github_btn.setText("\N{GLOBE WITH MERIDIANS} " + self.tr_("about.github"))
        self.donate_btn.setText("\N{SPARKLING HEART} " + self.tr_("about.donate"))
        self.made_with.setText(self.tr_("about.made_with"))
