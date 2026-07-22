"""Resources & Tools: curated external links for Anycubic printer owners.

A single hub of useful destinations, opened in the system browser:

* **Model libraries** — where to find printable models. Includes Anycubic's own
  Makeronline plus cross-brand libraries (MakerWorld, Printables, Thingiverse).
  MakerWorld models are Bambu Lab's, but their ``.3mf`` files print fine on
  Anycubic machines after a quick pass through Anycubic Slicer Next.
* **AI model generators** — text/image-to-3D tools (Meshy and alternatives).
* **Slicers** — software compatible with Anycubic Kobra printers, with the
  official Anycubic Slicer Next first.
* **Anycubic** — official EU store, firmware/software and wiki.
* **Fleet management** — for owners running many printers at once, a link to
  a dedicated print-farm manager (this toolkit stays single-user by design).

Only canonical, tracking-free URLs are used.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout

from anycubic_toolkit import __anycubic_wiki__
from anycubic_toolkit.core.websources import firmware_url, makeronline_url
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card

_GLOBE = "\N{GLOBE WITH MERIDIANS}"
_DOWNLOAD = "\N{DOWNWARDS BLACK ARROW}"

# (label, url) — proper nouns, not translated. URLs are canonical/tracking-free.
_MODEL_LIBRARIES = [
    ("Makeronline", makeronline_url()),
    ("MakerWorld", "https://makerworld.com/"),
    ("Printables", "https://www.printables.com/"),
    ("Thingiverse", "https://www.thingiverse.com/"),
]
_AI_GENERATORS = [
    ("Meshy AI", "https://www.meshy.ai/"),
    ("Tripo AI", "https://www.tripo3d.ai/"),
    ("Rodin (Hyper3D)", "https://hyper3d.ai/"),
    ("Sloyd", "https://www.sloyd.ai/"),
    ("3D AI Studio", "https://www.3daistudio.com/"),
]
_SLICERS = [
    ("Anycubic Slicer Next", firmware_url()),
    ("OrcaSlicer", "https://github.com/SoftFever/OrcaSlicer/releases/latest"),
    ("UltiMaker Cura", "https://ultimaker.com/software/ultimaker-cura/"),
    ("PrusaSlicer", "https://www.prusa3d.com/prusaslicer/"),
    ("Bambu Studio", "https://bambulab.com/en/download/studio"),
]
_ANYCUBIC_LINKS = [
    ("Anycubic EU", "https://eu.anycubic.com/"),
    ("Anycubic — Firmware & Software", firmware_url()),
    ("Anycubic Wiki", __anycubic_wiki__),
]
# For users running many printers (a real "farm"), not just one or two — this
# toolkit deliberately stays a single-user diagnostics app and doesn't try to
# grow into a fleet-dispatch system, so it just points to a dedicated one.
_FLEET_TOOLS = [
    ("Print Farm Manager (GitHub)", "https://github.com/joeltelling/print-farm-manager"),
]


class ResourcesPage(ModulePage):
    """Curated links to model libraries, AI generators, slicers and Anycubic."""

    title_key = "resources.title"
    subtitle_key = "resources.subtitle"
    help_key = "resources.help"

    def build(self) -> None:
        self._sections: list[tuple[Card, QLabel, str]] = []
        self._add_section("resources.libraries", "resources.libraries_note", _MODEL_LIBRARIES, _GLOBE)
        self._add_section("resources.ai", "resources.ai_note", _AI_GENERATORS, _GLOBE)
        self._add_section("resources.slicers", "resources.slicers_note", _SLICERS, _DOWNLOAD)
        self._add_section("resources.anycubic", "resources.anycubic_note", _ANYCUBIC_LINKS, _GLOBE)
        self._add_section("resources.fleet", "resources.fleet_note", _FLEET_TOOLS, _GLOBE)
        self.content_layout.addStretch(1)

    def _add_section(
        self,
        title_key: str,
        note_key: str,
        entries: list[tuple[str, str]],
        glyph: str,
    ) -> None:
        card = Card()
        body = card.body_layout()
        note = QLabel()
        note.setObjectName("Muted")
        note.setWordWrap(True)
        body.addWidget(note)

        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        for label, url in entries:
            button = QPushButton(f"{glyph}  {label}")
            button.setObjectName("Link")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            buttons.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addLayout(buttons)

        self.content_layout.addWidget(card)
        self._sections.append((card, note, title_key))
        # Remember the note key alongside the label for retranslate.
        note.setProperty("note_key", note_key)

    def retranslate(self) -> None:
        super().retranslate()
        for card, note, title_key in self._sections:
            card.set_title(self.tr_(title_key))
            note.setText(self.tr_(str(note.property("note_key"))))
