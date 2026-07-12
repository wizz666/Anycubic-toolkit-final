"""Home Assistant bridge via MQTT Discovery.

Publishes the printer's live telemetry to a Home Assistant-connected MQTT
broker (e.g. HA's built-in Mosquitto) using the open, documented
`MQTT Discovery <https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery>`_
convention. Home Assistant then creates the sensors automatically — no custom
component or Rinkhals required.

The toolkit acts purely as a **publisher of status** (read-only): it announces
a set of sensors once, then pushes a single JSON state payload that each sensor
reads through a value template. Requires ``paho-mqtt``; without it the bridge
reports that cleanly instead of failing.

This is a clean-room implementation of a public protocol; no third-party code
is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from anycubic_toolkit import __app_name__, __homepage__, __version__
from anycubic_toolkit.core.telemetry import PrinterSnapshot

DISCOVERY_PREFIX = "homeassistant"
_BASE_TOPIC = "anycubic_toolkit"

# (key, name, unit, device_class, value_template-field, icon)
_SENSORS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("state", "State", "", "", "state", "mdi:printer-3d"),
    ("progress", "Progress", "%", "", "progress", "mdi:progress-clock"),
    ("nozzle", "Nozzle temperature", "\u00b0C", "temperature", "nozzle_temp", "mdi:printer-3d-nozzle"),
    ("bed", "Bed temperature", "\u00b0C", "temperature", "bed_temp", "mdi:radiator"),
    ("layer", "Layer", "", "", "layer", "mdi:layers"),
    ("remaining", "Time remaining", "min", "duration", "remaining_minutes", "mdi:timer-sand"),
    ("file", "File", "", "", "filename", "mdi:file"),
)


@dataclass
class HaConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""

    @staticmethod
    def from_config(cfg) -> "HaConfig":
        try:
            port = int(cfg.get("ha_port", 1883) or 1883)
        except (TypeError, ValueError):
            port = 1883
        return HaConfig(
            enabled=bool(cfg.get("ha_enabled", False)),
            host=str(cfg.get("ha_host", "")),
            port=port,
            username=str(cfg.get("ha_username", "")),
            password=str(cfg.get("ha_password", "")),
        )

    def is_ready(self) -> bool:
        return bool(self.enabled and self.host.strip())


class HomeAssistantPublisher:
    """Publishes snapshots to Home Assistant over MQTT Discovery."""

    def __init__(self, config: HaConfig) -> None:
        self.config = config
        self._announced: set[str] = set()

    def _node_id(self, snapshot: PrinterSnapshot) -> str:
        raw = snapshot.printer_id or snapshot.model or "printer"
        return "".join(c if c.isalnum() else "_" for c in raw).strip("_").lower() or "printer"

    def test_connection(self) -> tuple[bool, str]:
        """Try connecting to the broker; return (ok, message-key)."""
        client = self._make_client()
        if client is None:
            return False, "ha_missing_mqtt"
        try:
            client.connect(self.config.host, self.config.port, keepalive=15)
            client.disconnect()
        except (OSError, TimeoutError) as exc:
            return False, f"unreachable: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface any broker error cleanly
            return False, f"error: {exc}"
        return True, "ok"

    def publish(self, snapshot: PrinterSnapshot) -> bool:
        """Announce sensors (once) and push the current state. Best-effort."""
        if not self.config.is_ready():
            return False
        client = self._make_client()
        if client is None:
            return False
        node = self._node_id(snapshot)
        state_topic = f"{_BASE_TOPIC}/{node}/state"
        avail_topic = f"{_BASE_TOPIC}/{node}/availability"
        try:
            client.connect(self.config.host, self.config.port, keepalive=15)
            client.loop_start()
            if node not in self._announced:
                self._announce(client, snapshot, node, state_topic, avail_topic)
                self._announced.add(node)
            client.publish(avail_topic, "online", retain=True)
            client.publish(state_topic, json.dumps(_state_payload(snapshot)), retain=True)
        except (OSError, TimeoutError):
            return False
        except Exception:  # noqa: BLE001 - never let publishing break the app
            return False
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return True

    # ------------------------------------------------------------- internal

    def _make_client(self):
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415 - optional dependency
        except ImportError:
            return None
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"anycubic-toolkit-ha-{id(self) & 0xFFFF:x}",
        )
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        return client

    def _announce(self, client, snapshot, node, state_topic, avail_topic) -> None:
        device = {
            "identifiers": [f"{_BASE_TOPIC}_{node}"],
            "name": snapshot.model or f"Anycubic {node}",
            "manufacturer": "Anycubic",
            "model": snapshot.model or "3D printer",
            "sw_version": snapshot.firmware or __version__,
            "configuration_url": __homepage__,
        }
        for key, name, unit, device_class, field, icon in _SENSORS:
            config = {
                "name": name,
                "unique_id": f"{_BASE_TOPIC}_{node}_{key}",
                "object_id": f"anycubic_{node}_{key}",
                "state_topic": state_topic,
                "availability_topic": avail_topic,
                "value_template": f"{{{{ value_json.{field} }}}}",
                "icon": icon,
                "device": device,
                "origin": {"name": __app_name__, "sw_version": __version__},
            }
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            topic = f"{DISCOVERY_PREFIX}/sensor/{_BASE_TOPIC}_{node}/{key}/config"
            client.publish(topic, json.dumps(config), retain=True)


def _state_payload(snapshot: PrinterSnapshot) -> dict:
    layer = (
        f"{snapshot.current_layer}/{snapshot.total_layers}"
        if snapshot.total_layers
        else ""
    )
    return {
        "state": snapshot.state,
        "progress": snapshot.progress_pct,
        "nozzle_temp": snapshot.nozzle_temp,
        "bed_temp": snapshot.bed_temp,
        "layer": layer,
        "remaining_minutes": snapshot.remaining_minutes,
        "filename": snapshot.filename,
    }
