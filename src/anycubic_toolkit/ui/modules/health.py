"""Printer Health: overall and per-component scores from the last analysis."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card, ScoreBar

# Mapping of component id -> translation key for its display label.
_COMPONENT_LABELS: dict[str, str] = {
    "extruder": "health.extruder",
    "ace": "health.ace",
    "bed": "health.bed",
    "temperature": "health.temperature",
    "fans": "health.fans",
    "motors": "health.motors",
}


class HealthPage(ModulePage):
    """Visualizes subsystem health as color-coded bars."""

    title_key = "health.title"
    subtitle_key = "health.subtitle"
    help_key = "health.help"

    def build(self) -> None:
        self.overall_card = Card()
        self.overall_bar = ScoreBar()
        self.overall_card.body_layout().addWidget(self.overall_bar)
        self.content_layout.addWidget(self.overall_card)

        self.components_card = Card()
        self._components_body = self.components_card.body_layout()
        self._bars: dict[str, ScoreBar] = {}
        for component in _COMPONENT_LABELS:
            bar = ScoreBar()
            self._bars[component] = bar
            self._components_body.addWidget(bar)
        self.content_layout.addWidget(self.components_card)

        self.empty_label = QLabel()
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        self.content_layout.addWidget(self.empty_label)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._refresh()

    def retranslate(self) -> None:
        super().retranslate()
        self.overall_card.set_title(self.tr_("health.overall"))
        self.components_card.set_title(self.tr_("app.name"))
        self._refresh()

    # ------------------------------------------------------------- internal

    def _refresh(self) -> None:
        analysis = self.ctx.last_analysis
        has_data = analysis is not None and bool(analysis.components)

        self.overall_card.setVisible(has_data)
        self.components_card.setVisible(has_data)
        self.empty_label.setVisible(not has_data)

        if not has_data:
            self.empty_label.setText(self.tr_("health.no_data"))
            return

        assert analysis is not None
        self.overall_bar.set_score(
            self.tr_("health.overall"), analysis.overall_score
        )

        scores = {c.component: c for c in analysis.components}
        for component, bar in self._bars.items():
            score = scores.get(component)
            label = self.tr_(_COMPONENT_LABELS[component])
            if score is None:
                bar.set_score(label, 100, "")
                continue
            bar.set_score(
                label,
                score.score,
                self.tr_("health.issues", count=score.issues),
            )
