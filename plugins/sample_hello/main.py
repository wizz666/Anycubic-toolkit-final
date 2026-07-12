"""Hello Sample plugin.

The smallest useful plugin: it contributes one page to the sidebar. Copy this
folder, rename it and edit ``plugin.json`` to build your own plugin.

A plugin module must define a ``create_plugin(context)`` factory that returns
a :class:`ToolkitPlugin` instance. The *context* dict provides shared
services: ``config``, ``translator``, ``theme``, ``api`` and ``app_version``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from anycubic_toolkit.core.plugins import ToolkitPlugin


class HelloPage(QWidget):
    """A trivial page shown when the plugin is enabled."""

    def __init__(self, context: dict[str, Any]) -> None:
        super().__init__()
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = QLabel("Hello from a plugin!")
        title.setObjectName("PageTitle")
        version = context.get("app_version", "?")
        body = QLabel(
            "This page is provided by the bundled <b>Hello Sample</b> plugin.<br>"
            f"It is running inside Anycubic Toolkit v{version}.<br><br>"
            "Duplicate <code>plugins/sample_hello</code> to start your own plugin."
        )
        body.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(body)


class HelloPlugin(ToolkitPlugin):
    """Example plugin contributing a single sidebar page."""

    def sidebar_title(self) -> str:
        return "Hello Sample"

    def sidebar_icon(self) -> str:
        return "\N{WAVING HAND SIGN}"

    def create_page(self) -> QWidget:
        return HelloPage(self.context)


def create_plugin(context: dict[str, Any]) -> ToolkitPlugin:
    """Factory called by the plugin manager."""
    return HelloPlugin(context)
