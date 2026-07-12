"""Reusable UI building blocks shared by all modules."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    """Rounded surface used to group related content."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(10)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setVisible(bool(title))
        self._layout.addWidget(self.title_label)

    def set_title(self, title: str) -> None:
        """Update the card heading."""
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))

    def body_layout(self) -> QVBoxLayout:
        """Layout below the title where callers add content."""
        return self._layout


class StatTile(Card):
    """Dashboard tile showing one big value with a caption."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("BigNumber")
        self.value_label.setWordWrap(True)
        self.caption_label = QLabel("")
        self.caption_label.setObjectName("Muted")
        self.caption_label.setWordWrap(True)
        layout = self.body_layout()
        layout.addWidget(self.value_label)
        layout.addWidget(self.caption_label)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_stat(self, value: str, caption: str) -> None:
        """Update the displayed value and caption."""
        self.value_label.setText(value)
        self.caption_label.setText(caption)


class ScoreBar(QWidget):
    """Labelled progress bar with color coding for health scores."""

    def __init__(self, label: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.label = QLabel(label)
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)

    def set_score(self, label: str, score: int, detail: str = "") -> None:
        """Update label, value and color (green / amber / red)."""
        self.label.setText(f"{label}  —  {score}/100")
        self.bar.setValue(max(0, min(score, 100)))
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        if score >= 80:
            color = "#4CAF7D"
        elif score >= 50:
            color = "#E0A32E"
        else:
            color = "#E05252"
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 5px; }}"
        )


class DropZone(QFrame):
    """Dashed drop target that accepts a single file via drag & drop."""

    file_dropped = Signal(str)

    def __init__(self, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label = QLabel("\N{OPEN FILE FOLDER}")
        self.icon_label.setStyleSheet("font-size: 34px;")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("Muted")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.hint_label)

    def set_hint(self, hint: str) -> None:
        """Change the hint text (used on language switch)."""
        self.hint_label.setText(hint)

    # ------------------------------------------------------------- Qt events

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.file_dropped.emit(local)
        event.acceptProposedAction()


def clear_layout(layout) -> None:
    """Remove and delete every item in a layout (recursively)."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


# Type alias for retranslation callbacks used across pages.
Retranslate = Callable[[], None]
