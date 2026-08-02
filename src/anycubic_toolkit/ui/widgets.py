"""Reusable UI building blocks shared by all modules."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPaintEvent, QPen
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
    """Dashed drop target that accepts one or more files/folders via drag &
    drop.

    ``file_dropped`` fires with the first path for existing single-file
    callers; pass ``multiple=True`` to also get every dropped path (files and
    folders both) via ``files_dropped``.
    """

    file_dropped = Signal(str)
    files_dropped = Signal(list)

    def __init__(
        self, hint: str = "", parent: QWidget | None = None, *, multiple: bool = False
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._multiple = multiple
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
        locals_ = [u.toLocalFile() for u in urls if u.toLocalFile()]
        if locals_:
            if self._multiple:
                self.files_dropped.emit(locals_)
            self.file_dropped.emit(locals_[0])
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


class TempGraph(QWidget):
    """Rolling line chart of nozzle and bed temperatures.

    Feed it one reading at a time with :meth:`add_sample`; it keeps the most
    recent *max_samples* points and paints two solid lines (current temps) plus
    dashed target lines. Colors follow the classic convention — warm orange for
    the nozzle, cool blue for the bed — and are readable on both themes.
    """

    NOZZLE_COLOR = "#ff8c42"
    BED_COLOR = "#4f9dff"
    GRID_COLOR = "#80808040"

    def __init__(self, max_samples: int = 120, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max = max(10, max_samples)
        self._samples: list[tuple[float, float, float, float]] = []
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def clear(self) -> None:
        """Drop all samples (e.g. when switching printers)."""
        self._samples.clear()
        self.update()

    def add_sample(
        self, nozzle: float, bed: float, nozzle_target: float = 0.0, bed_target: float = 0.0
    ) -> None:
        """Append one reading and repaint."""
        self._samples.append((nozzle, bed, nozzle_target, bed_target))
        if len(self._samples) > self._max:
            del self._samples[: len(self._samples) - self._max]
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(6, 6, -6, -18)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        peak = 60.0
        for nozzle, bed, nt, bt in self._samples:
            peak = max(peak, nozzle, bed, nt, bt)
        peak *= 1.1

        grid_pen = QPen(QColor(self.GRID_COLOR))
        grid_pen.setWidth(1)
        painter.setPen(grid_pen)
        steps = 4
        for i in range(steps + 1):
            y = rect.bottom() - rect.height() * i / steps
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
            label = f"{peak * i / steps:.0f}\N{DEGREE SIGN}"
            painter.drawText(rect.left() + 2, int(y) - 3, label)

        if len(self._samples) < 2:
            return

        def x_at(index: int) -> float:
            return rect.left() + rect.width() * index / (self._max - 1)

        def y_at(value: float) -> float:
            return rect.bottom() - rect.height() * min(value, peak) / peak

        series = (
            (0, self.NOZZLE_COLOR, Qt.PenStyle.SolidLine, 2),   # nozzle temp
            (1, self.BED_COLOR, Qt.PenStyle.SolidLine, 2),      # bed temp
            (2, self.NOZZLE_COLOR, Qt.PenStyle.DashLine, 1),    # nozzle target
            (3, self.BED_COLOR, Qt.PenStyle.DashLine, 1),       # bed target
        )
        start = self._max - len(self._samples)
        for column, color, style, width in series:
            values = [sample[column] for sample in self._samples]
            if column >= 2 and not any(values):
                continue  # skip flat zero target lines
            pen = QPen(QColor(color))
            pen.setWidthF(width)
            pen.setStyle(style)
            painter.setPen(pen)
            previous = None
            for index, value in enumerate(values):
                point = (x_at(start + index), y_at(value))
                if previous is not None:
                    painter.drawLine(
                        int(previous[0]), int(previous[1]), int(point[0]), int(point[1])
                    )
                previous = point
