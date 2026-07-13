"""Animated splash screen.

Shows a fade-in logo, live loading progress with the current task, the
version number, and three buttons: Continue (enabled as soon as loading
finishes), GitHub Sponsors and Ko-fi. Continue always works — donating is
optional and simply opens the browser.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from anycubic_toolkit import __sponsor_github__, __sponsor_kofi__, __version__
from anycubic_toolkit.core.i18n import Translator


class SplashScreen(QWidget):
    """Frameless, rounded splash window."""

    continue_requested = Signal()

    def __init__(self, translator: Translator) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen,
        )
        self.translator = translator
        self.setObjectName("Splash")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(460, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 32, 36, 28)
        layout.setSpacing(10)

        self.logo = QLabel("\N{PRINTER}")
        self.logo.setStyleSheet("font-size: 42px;")
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title = QLabel("Anycubic Toolkit")
        self.title.setObjectName("PageTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.version = QLabel(f"v{__version__}")
        self.version.setObjectName("Muted")
        self.version.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)

        self.task = QLabel(translator.tr("splash.loading"))
        self.task.setObjectName("Muted")
        self.task.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.hint = QLabel(translator.tr("splash.donate_hint"))
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.btn_sponsors = QPushButton(
            "\N{SPARKLING HEART} " + translator.tr("splash.github_sponsors")
        )
        self.btn_kofi = QPushButton(
            "\N{HOT BEVERAGE} " + translator.tr("splash.kofi")
        )
        self.btn_continue = QPushButton(translator.tr("splash.continue"))
        self.btn_continue.setObjectName("Primary")
        self.btn_continue.setEnabled(False)
        buttons.addWidget(self.btn_sponsors)
        buttons.addWidget(self.btn_kofi)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_continue)

        layout.addWidget(self.logo)
        layout.addWidget(self.title)
        layout.addWidget(self.version)
        layout.addSpacing(6)
        layout.addWidget(self.progress)
        layout.addWidget(self.task)
        layout.addStretch(1)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)

        self.btn_sponsors.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(__sponsor_github__))
        )
        self.btn_kofi.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(__sponsor_kofi__))
        )
        self.btn_continue.clicked.connect(self.continue_requested.emit)

        self._fade_in()

    # ------------------------------------------------------------------ API

    def set_progress(self, value: int, task: str) -> None:
        """Update the loading bar and current task description."""
        self.progress.setValue(max(0, min(value, 100)))
        self.task.setText(task)
        if value >= 100:
            self.btn_continue.setEnabled(True)
            self.btn_continue.setFocus()

    # ------------------------------------------------------------- internal

    def _fade_in(self) -> None:
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        self._animation = QPropertyAnimation(effect, b"opacity", self)
        self._animation.setDuration(450)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(lambda: self.setGraphicsEffect(None))
        self._animation.start()
