"""Plugin system.

A plugin is a folder containing::

    my_plugin/
        plugin.json        # metadata (see PluginManifest)
        main.py            # must define create_plugin(context) -> ToolkitPlugin
        resources/
            icon.png       # optional sidebar/marketplace icon

Plugins are discovered from two locations:

* the ``plugins/`` folder next to the application (bundled plugins)
* the per-user data directory (Marketplace-installed plugins)

Enable state is persisted in the configuration file. A plugin may contribute
a full page to the sidebar by returning a QWidget from ``create_page()``.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anycubic_toolkit.core.config import ConfigManager, user_plugins_dir

if TYPE_CHECKING:  # avoid a hard Qt dependency at import time for tests
    from PySide6.QtWidgets import QWidget


@dataclass
class PluginManifest:
    """Parsed contents of ``plugin.json``."""

    plugin_id: str
    name: str
    version: str
    description: str
    author: str
    icon: str
    min_app_version: str
    path: Path

    @staticmethod
    def load(folder: Path) -> "PluginManifest | None":
        """Read and validate ``plugin.json`` inside *folder*."""
        manifest_file = folder / "plugin.json"
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        plugin_id = data.get("id") or folder.name
        return PluginManifest(
            plugin_id=str(plugin_id),
            name=str(data.get("name", plugin_id)),
            version=str(data.get("version", "0.0.0")),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            icon=str(data.get("icon", "resources/icon.png")),
            min_app_version=str(data.get("min_app_version", "0.0.0")),
            path=folder,
        )


class ToolkitPlugin:
    """Base class all plugins inherit from.

    Subclasses override the lifecycle hooks they need. The *context* dict
    gives access to shared services: ``config``, ``translator``, ``api``,
    ``theme`` and ``app_version``.
    """

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context

    def on_load(self) -> None:
        """Called once after the plugin module is imported and enabled."""

    def on_unload(self) -> None:
        """Called when the plugin is disabled or the app shuts down."""

    def create_page(self) -> "QWidget | None":
        """Return a QWidget to appear as a sidebar page, or ``None``."""
        return None

    def sidebar_title(self) -> str:
        """Title shown in the sidebar if :meth:`create_page` returns a widget."""
        return self.__class__.__name__

    def sidebar_icon(self) -> str:
        """Unicode glyph shown next to the sidebar title."""
        return "\N{ELECTRIC PLUG}"


@dataclass
class LoadedPlugin:
    """A discovered plugin plus its runtime state."""

    manifest: PluginManifest
    instance: ToolkitPlugin | None = None
    enabled: bool = False
    error: str = ""


class PluginManager:
    """Discovers, loads, enables and removes plugins."""

    def __init__(self, config: ConfigManager, context: dict[str, Any]) -> None:
        self.config = config
        self.context = context
        self.plugins: dict[str, LoadedPlugin] = {}

    # ------------------------------------------------------------ discovery

    def search_paths(self) -> list[Path]:
        """Folders scanned for plugins, in priority order."""
        bundled = _application_root() / "plugins"
        return [bundled, user_plugins_dir()]

    def discover(self) -> None:
        """Scan search paths, load manifests and activate enabled plugins."""
        enabled_map: dict[str, bool] = self.config.get("plugins_enabled", {}) or {}
        for base in self.search_paths():
            if not base.is_dir():
                continue
            for folder in sorted(base.iterdir()):
                if not folder.is_dir() or not (folder / "plugin.json").exists():
                    continue
                manifest = PluginManifest.load(folder)
                if manifest is None or manifest.plugin_id in self.plugins:
                    continue
                loaded = LoadedPlugin(manifest=manifest)
                self.plugins[manifest.plugin_id] = loaded
                if enabled_map.get(manifest.plugin_id, False):
                    self.enable(manifest.plugin_id, persist=False)

    # ------------------------------------------------------------ lifecycle

    def enable(self, plugin_id: str, persist: bool = True) -> bool:
        """Import and activate a plugin. Returns True on success."""
        loaded = self.plugins.get(plugin_id)
        if loaded is None:
            return False
        if loaded.instance is not None:
            loaded.enabled = True
        else:
            try:
                loaded.instance = self._instantiate(loaded.manifest)
                loaded.instance.on_load()
                loaded.enabled = True
                loaded.error = ""
            except Exception as exc:  # noqa: BLE001 - shown in the UI
                loaded.error = str(exc)
                loaded.enabled = False
        if persist:
            self._persist_enabled()
        return loaded.enabled

    def disable(self, plugin_id: str, persist: bool = True) -> None:
        """Deactivate a plugin."""
        loaded = self.plugins.get(plugin_id)
        if loaded is None:
            return
        if loaded.instance is not None:
            try:
                loaded.instance.on_unload()
            except Exception:  # noqa: BLE001 - never let a plugin crash shutdown
                pass
            loaded.instance = None
        loaded.enabled = False
        if persist:
            self._persist_enabled()

    def shutdown(self) -> None:
        """Unload all plugins (application exit)."""
        for plugin_id in list(self.plugins):
            self.disable(plugin_id, persist=False)

    # ---------------------------------------------------------- marketplace

    def install_from_zip(self, zip_path: Path) -> PluginManifest:
        """Install a downloaded plugin archive into the user plugin folder."""
        target_root = user_plugins_dir()
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            manifest_entries = [n for n in names if n.endswith("plugin.json")]
            if not manifest_entries:
                raise ValueError("Archive does not contain a plugin.json manifest.")
            prefix = manifest_entries[0][: -len("plugin.json")]
            folder_name = prefix.strip("/").split("/")[0] if prefix.strip("/") else zip_path.stem
            destination = target_root / folder_name
            if destination.exists():
                shutil.rmtree(destination)
            for name in names:
                if not name.startswith(prefix) and prefix:
                    continue
                relative = name[len(prefix):] if prefix else name
                if not relative or relative.endswith("/"):
                    continue
                out = destination / relative
                _guard_path(target_root, out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(archive.read(name))
        manifest = PluginManifest.load(destination)
        if manifest is None:
            shutil.rmtree(destination, ignore_errors=True)
            raise ValueError("Installed plugin has an invalid plugin.json manifest.")
        self.plugins[manifest.plugin_id] = LoadedPlugin(manifest=manifest)
        return manifest

    def remove(self, plugin_id: str) -> None:
        """Disable and delete a user-installed plugin from disk."""
        loaded = self.plugins.get(plugin_id)
        if loaded is None:
            return
        self.disable(plugin_id)
        if user_plugins_dir() in loaded.manifest.path.parents:
            shutil.rmtree(loaded.manifest.path, ignore_errors=True)
        del self.plugins[plugin_id]
        enabled_map = self.config.get("plugins_enabled", {}) or {}
        enabled_map.pop(plugin_id, None)
        self.config.set("plugins_enabled", enabled_map)

    # ------------------------------------------------------------- internal

    def _instantiate(self, manifest: PluginManifest) -> ToolkitPlugin:
        main_file = manifest.path / "main.py"
        if not main_file.exists():
            raise FileNotFoundError(f"{manifest.plugin_id}: main.py is missing.")
        module_name = f"anycubic_toolkit_plugin_{manifest.plugin_id}"
        spec = importlib.util.spec_from_file_location(module_name, main_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create import spec for {main_file}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        factory = getattr(module, "create_plugin", None)
        if not callable(factory):
            raise AttributeError(
                f"{manifest.plugin_id}: main.py must define create_plugin(context)."
            )
        instance = factory(dict(self.context))
        if not isinstance(instance, ToolkitPlugin):
            raise TypeError(
                f"{manifest.plugin_id}: create_plugin() must return a ToolkitPlugin."
            )
        return instance

    def _persist_enabled(self) -> None:
        self.config.set(
            "plugins_enabled",
            {plugin_id: loaded.enabled for plugin_id, loaded in self.plugins.items()},
        )


def _application_root() -> Path:
    """Folder containing the application (source tree or frozen bundle)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _guard_path(root: Path, candidate: Path) -> None:
    """Prevent zip-slip: *candidate* must stay inside *root*."""
    if root.resolve() not in candidate.resolve().parents:
        raise ValueError(f"Unsafe path in plugin archive: {candidate}")
