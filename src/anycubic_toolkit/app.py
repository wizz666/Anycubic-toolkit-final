"""Application entry point.

Wires together the core services, shows the animated splash screen while
they initialize, and reveals the main window when the user clicks Continue.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from anycubic_toolkit import __app_name__, __version__
from anycubic_toolkit.core.api import WizzApiClient
from anycubic_toolkit.core.assets import app_icon_path
from anycubic_toolkit.core.config import ConfigManager
from anycubic_toolkit.core.i18n import Translator
from anycubic_toolkit.core.passwords import PasswordService
from anycubic_toolkit.core.plugins import PluginManager
from anycubic_toolkit.core.themes import ThemeManager
from anycubic_toolkit.core.updater import check_for_updates
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules import AppContext
from anycubic_toolkit.ui.splash import SplashScreen


class Application:
    """Owns the top-level services and window lifecycle."""

    def __init__(self, qapp: QApplication) -> None:
        self.qapp = qapp
        self.config = ConfigManager()
        self.translator = Translator(self.config.get("language", "en"))
        self.theme = ThemeManager(self.config.get("theme", "dark"))
        self.api = WizzApiClient()
        self.passwords = PasswordService(self.api)
        self.context = AppContext(
            config=self.config,
            translator=self.translator,
            theme=self.theme,
            api=self.api,
            passwords=self.passwords,
        )
        self.plugins = PluginManager(
            self.config,
            {
                "config": self.config,
                "translator": self.translator,
                "theme": self.theme,
                "api": self.api,
                "app_version": __version__,
            },
        )
        self.context.plugins = self.plugins
        self.splash: SplashScreen | None = None
        self.window = None

    # --------------------------------------------------------------- startup

    def start(self) -> None:
        """Show the splash and begin staged initialization."""
        self.splash = SplashScreen(self.translator)
        self.splash.continue_requested.connect(self._show_main_window)
        self._center(self.splash)
        self.splash.show()

        # Staged loading, each step updating the splash progress.
        steps = [
            (12, "splash.task_config", self._noop),
            (28, "splash.task_i18n", self._noop),
            (44, "splash.task_theme", self._noop),
            (66, "splash.task_plugins", self._load_plugins),
            (86, "splash.task_ui", self._build_window),
            (100, "splash.task_done", self._noop),
        ]
        self._run_steps(steps, 0)

    def _run_steps(self, steps: list, index: int) -> None:
        if index >= len(steps):
            return
        percent, key, action = steps[index]
        action()
        if self.splash is not None:
            self.splash.set_progress(percent, self.translator.tr(key))
        QTimer.singleShot(220, lambda: self._run_steps(steps, index + 1))

    # ----------------------------------------------------------------- steps

    @staticmethod
    def _noop() -> None:
        return None

    def _load_plugins(self) -> None:
        try:
            self.plugins.discover()
        except Exception:  # noqa: BLE001 - never let a plugin break startup
            pass

    def _build_window(self) -> None:
        from anycubic_toolkit.ui.main_window import MainWindow

        self.window = MainWindow(self.context)

    # -------------------------------------------------------------- lifecycle

    def _show_main_window(self) -> None:
        if self.window is None:
            self._build_window()
        assert self.window is not None
        self._center(self.window)
        self.window.show()
        if self.splash is not None:
            self.splash.close()
            self.splash = None
        if self.config.get("auto_update"):
            self._auto_update_check()

    def _auto_update_check(self) -> None:
        channel = str(self.config.get("update_channel"))
        worker = FunctionWorker(check_for_updates, self.api, channel)
        worker.signals.finished.connect(self._on_auto_update)
        # Silent on failure — the manual button reports errors.
        run_in_background(worker)

    def _on_auto_update(self, info) -> None:  # noqa: ANN001 - UpdateInfo
        if info.available and self.window is not None:
            self.window.page_heading.setToolTip(
                self.translator.tr("updates.available", version=info.latest_version)
            )

    def shutdown(self) -> None:
        """Unload plugins on application exit."""
        self.plugins.shutdown()

    # ----------------------------------------------------------------- utils

    def _center(self, widget) -> None:  # noqa: ANN001 - QWidget
        screen = self.qapp.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        frame = widget.frameGeometry()
        frame.moveCenter(geometry.center())
        widget.move(frame.topLeft())


def _apply_windows_taskbar_identity() -> None:
    """On Windows, give the app its own taskbar identity.

    Without an explicit AppUserModelID, Windows groups the process under the
    generic Python icon in the taskbar instead of the app's own icon.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        app_id = f"Wizz.AnycubicToolkit.{__version__}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # noqa: BLE001 - cosmetic only, never block startup
        pass


def main() -> int:
    """Create the QApplication and run the event loop."""
    _apply_windows_taskbar_identity()

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(__app_name__)
    qapp.setApplicationDisplayName(__app_name__)
    qapp.setApplicationVersion(__version__)

    icon_path = app_icon_path()
    if icon_path:
        qapp.setWindowIcon(QIcon(icon_path))

    application = Application(qapp)
    qapp.aboutToQuit.connect(application.shutdown)
    application.start()
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
